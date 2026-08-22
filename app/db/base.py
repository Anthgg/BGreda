"""Base declarativa de SQLAlchemy y mixins comunes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Convencion de nombres explicita: sin ella Alembic genera nombres anonimos
# para constraints e indices y las migraciones dejan de ser reversibles.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarativa comun a todos los modelos."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Trazabilidad minima y generica de creacion/actualizacion.

    Es la unica infraestructura de auditoria que introduce la Fase 1. La
    auditoria funcional completa corresponde a fases posteriores.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
