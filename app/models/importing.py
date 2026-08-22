"""Importacion controlada de maestros desde Excel.

El XLSX no es una base de datos: se lee una vez, se convierte en filas de
staging y a partir de ahi todo ocurre en PostgreSQL. El archivo no vuelve a
consultarse en ningun request posterior.

El preview vive en ``import_rows`` y jamas toca los maestros. Solo el commit
—transaccional y explicito— escribe en productos, terceros o inventario.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImportStatus(StrEnum):
    UPLOADED = "UPLOADED"
    ANALYZED = "ANALYZED"
    READY = "READY"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ImportEntity(StrEnum):
    PRODUCT_CATEGORY = "PRODUCT_CATEGORY"
    POS_CATEGORY = "POS_CATEGORY"
    UNIT = "UNIT"
    PRODUCT = "PRODUCT"
    PARTNER = "PARTNER"
    LOCATION = "LOCATION"
    STOCK = "STOCK"
    #: Se analiza y se conserva en crudo, pero no se importa en Fase 3.
    RECIPE = "RECIPE"


class ImportAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    SKIP = "SKIP"
    ERROR = "ERROR"


class ImportRowStatus(StrEnum):
    READY = "READY"
    #: Necesita una decision humana antes de poder confirmarse.
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"
    COMMITTED = "COMMITTED"


class ImportBatch(Base):
    """Una ejecucion del importador, desde la subida hasta el commit."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: SHA-256 del archivo tal cual llego. Permite avisar de una reimportacion
    #: identica sin necesidad de conservar el binario.
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ImportStatus] = mapped_column(String(16), nullable=False)

    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Datos del archivo que no forman parte del modelo final —cuentas
    #: contables de Odoo, recetas— conservados solo para trazabilidad.
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("file_hash ~ '^[0-9a-f]{64}$'", name="file_hash_sha256"),
        CheckConstraint("file_size > 0", name="file_size_positive"),
        CheckConstraint(
            "status IN ('UPLOADED', 'ANALYZED', 'READY', 'COMMITTED', 'FAILED', 'CANCELLED')",
            name="status_allowed",
        ),
        Index("ix_import_batches_hash", "file_hash"),
        Index("ix_import_batches_created", "created_at"),
    )


class ImportRow(Base):
    """Fila de staging: lo que el archivo dijo y lo que el sistema hara.

    ``raw`` conserva el valor original de cada celda —incluido el costo con
    dieciseis decimales que Excel arrastra— y ``normalized`` el valor que se
    escribira. El usuario compara ambos antes de confirmar.
    """

    __tablename__ = "import_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    entity: Mapped[ImportEntity] = mapped_column(String(24), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Orden de aplicacion dentro de la entidad (jerarquias primero).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    action: Mapped[ImportAction] = mapped_column(String(16), nullable=False)
    status: Mapped[ImportRowStatus] = mapped_column(String(24), nullable=False)

    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: Candidatos ofrecidos al usuario cuando la fila necesita una decision.
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: Decision tomada por el usuario en el preview.
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    target_table: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("batch_id", "entity", "source_row", name="uq_import_rows_batch_id"),
        CheckConstraint("source_row > 0", name="source_row_positive"),
        CheckConstraint(
            "action IN ('CREATE', 'UPDATE', 'SKIP', 'ERROR')",
            name="action_allowed",
        ),
        CheckConstraint(
            "status IN ('READY', 'REVIEW_REQUIRED', 'RESOLVED', 'BLOCKED', 'COMMITTED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "entity IN ('PRODUCT_CATEGORY', 'POS_CATEGORY', 'UNIT', 'PRODUCT', "
            "'PARTNER', 'LOCATION', 'STOCK', 'RECIPE')",
            name="entity_allowed",
        ),
        Index("ix_import_rows_batch_entity", "batch_id", "entity", "sort_order"),
        Index("ix_import_rows_status", "batch_id", "status"),
    )
