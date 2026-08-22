"""Perfil de usuario de la aplicacion.

``profiles`` es la lista de habilitacion del Cotizador Greda: existir en
Supabase Auth no basta para entrar. Cada fila representa a un usuario
autorizado y su rol dentro de la aplicacion.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserRole(StrEnum):
    """Roles iniciales. El modelo de permisos fino corresponde a fases futuras."""

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"


class Profile(Base, TimestampMixin):
    """Perfil de aplicacion asociado a un usuario de Supabase Auth."""

    __tablename__ = "profiles"

    #: Debe coincidir con ``auth.users.id`` de Supabase. La correspondencia la
    #: garantiza el backend: el identificador siempre proviene de un token ya
    #: verificado contra Supabase, nunca del cliente. No se declara una clave
    #: foranea al esquema ``auth`` para no acoplar las migraciones al esquema
    #: interno de Supabase.
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        String(20),
        nullable=False,
        server_default=UserRole.OPERATOR.value,
    )

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint(
            "role IN ('ADMIN', 'OPERATOR')",
            name="role_allowed",
        ),
        CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="display_name_not_blank",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuracion
        return f"<Profile id={self.id} role={self.role} active={self.active}>"
