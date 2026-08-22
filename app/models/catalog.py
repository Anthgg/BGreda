"""Catalogos controlados usados por la configuracion.

Las opciones se leen desde PostgreSQL. El frontend no mantiene listas
paralelas que puedan quedar desactualizadas y las claves foraneas impiden
guardar una moneda o un distrito inexistentes.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CurrencyCatalog(Base):
    """Codigo ISO 4217 vigente y su presentacion localizada para Peru."""

    __tablename__ = "currency_catalog"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    numeric_code: Mapped[str] = mapped_column(String(3), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    symbol: Mapped[str] = mapped_column(String(8), nullable=False)
    minor_units: Mapped[int | None] = mapped_column(SmallInteger)


class UbigeoDistrict(Base):
    """Distrito INEI con sus padres; el codigo de seis digitos es canonico."""

    __tablename__ = "ubigeo_districts"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    department_code: Mapped[str] = mapped_column(String(2), nullable=False)
    department_name: Mapped[str] = mapped_column(String(80), nullable=False)
    province_code: Mapped[str] = mapped_column(String(4), nullable=False)
    province_name: Mapped[str] = mapped_column(String(100), nullable=False)
    district_name: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (
        Index("ix_ubigeo_districts_department", "department_code", "department_name"),
        Index("ix_ubigeo_districts_province", "province_code", "province_name"),
    )


class SequencePatternPreset(Base, TimestampMixin):
    """Formato reutilizable de numeracion documental."""

    __tablename__ = "sequence_pattern_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    pattern: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
