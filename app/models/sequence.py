"""Secuencias documentales oficiales.

El correlativo de un documento es un dato legal y comercial: debe ser unico,
creciente e inmutable una vez asignado. Lo genera **exclusivamente** el backend
dentro de una transaccion; el frontend jamas lo propone ni lo incrementa.

La Fase 2 crea la configuracion, el contador y el servicio interno. El consumo
real se conecta cuando existan documentos: HR en Fase 4 y CTZ en Fase 5.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

#: Longitud maxima del prefijo configurable (CTZ, HR, GRE...).
MAX_PREFIX_LENGTH = 10
#: Rango admitido para el relleno con ceros.
MIN_PADDING = 1
MAX_PADDING = 12


class SequenceType(StrEnum):
    """Tipos de documento con correlativo propio.

    Cada tipo lleva su contador independiente: agotar cotizaciones no afecta a
    las quemas.
    """

    QUOTE = "QUOTE"
    FIRING = "FIRING"


class ResetPolicy(StrEnum):
    """Cuando vuelve el contador a 1.

    ``YEARLY`` implementa el formato aprobado en el Plan v1.2
    (``CTZ-2026-000001`` -> ``CTZ-2027-000001``). Las demas politicas existen
    porque el patron es configurable: el Documento Funcional describe un formato
    con mes y dia, alcanzable sin tocar codigo.
    """

    NEVER = "NEVER"
    YEARLY = "YEARLY"
    MONTHLY = "MONTHLY"
    DAILY = "DAILY"


class DocumentSequence(Base, TimestampMixin):
    """Configuracion y contador de una secuencia documental."""

    __tablename__ = "document_sequences"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)

    sequence_type: Mapped[SequenceType] = mapped_column(String(16), nullable=False, unique=True)

    prefix: Mapped[str] = mapped_column(String(MAX_PREFIX_LENGTH), nullable=False)

    #: Plantilla con marcadores: {PREFIX}, {YYYY}, {YY}, {MM}, {DD}, {NUMBER}.
    pattern: Mapped[str] = mapped_column(String(120), nullable=False)

    padding: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    reset_policy: Mapped[ResetPolicy] = mapped_column(String(16), nullable=False)

    #: Ultimo numero entregado dentro del periodo vigente. 0 = ninguno todavia.
    current_value: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Identifica el periodo del contador ("2026", "2026-08", "2026-08-22" o
    #: "" cuando la politica es NEVER). Al cambiar de periodo el contador
    #: reinicia sin colisionar, porque la unicidad incluye el periodo.
    period_key: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("''"))

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint(
            "sequence_type IN ('QUOTE', 'FIRING')",
            name="type_allowed",
        ),
        CheckConstraint(
            "reset_policy IN ('NEVER', 'YEARLY', 'MONTHLY', 'DAILY')",
            name="reset_policy_allowed",
        ),
        CheckConstraint(
            f"padding BETWEEN {MIN_PADDING} AND {MAX_PADDING}",
            name="padding_range",
        ),
        CheckConstraint("current_value >= 0", name="current_value_not_negative"),
        CheckConstraint("length(btrim(prefix)) > 0", name="prefix_not_blank"),
        CheckConstraint("position('{NUMBER}' in pattern) > 0", name="pattern_has_number"),
        CheckConstraint("version > 0", name="version_positive"),
    )


class DocumentSequenceIssue(Base):
    """Registro inmutable de cada numero entregado.

    Es la garantia de unicidad a nivel de base de datos y la evidencia de que
    un correlativo no se reutiliza: aunque el documento se cancele, la fila
    permanece. Tambien conserva el texto renderizado con el formato vigente en
    el momento de la emision, de modo que cambiar el prefijo o el padding mas
    adelante no reescribe la historia.
    """

    __tablename__ = "document_sequence_issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    sequence_type: Mapped[SequenceType] = mapped_column(String(16), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Valor final entregado, por ejemplo "CTZ-2026-000001".
    formatted_value: Mapped[str] = mapped_column(String(160), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    issued_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    __table_args__ = (
        # Dos transacciones simultaneas jamas pueden obtener el mismo numero:
        # si el contador fallara, esta restriccion aborta la segunda.
        UniqueConstraint("sequence_type", "period_key", "number", name="unique_number"),
        UniqueConstraint("formatted_value", name="unique_formatted"),
        CheckConstraint("number > 0", name="number_positive"),
    )
