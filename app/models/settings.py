"""Configuracion de empresa y parametros comerciales.

Ambas tablas son *singleton*: la aplicacion gestiona una unica empresa. La
unicidad no se confia al codigo, la impone la base de datos mediante una clave
primaria fija con CHECK. No se implementa multitenancy porque el proyecto no lo
requiere.

Los valores de negocio viven aqui y no en el codigo: cambiar el IGV, la moneda,
la vigencia o cualquier texto comercial no debe exigir un despliegue.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import percentage_numeric
from app.db.base import Base, TimestampMixin

#: Identificador de la unica fila de cada tabla de configuracion.
SINGLETON_ID = 1

#: Limite tecnico del IGV. No es una regla tributaria: es una barrera contra
#: errores de captura (un 1800 por confundir fraccion con porcentaje).
MAX_TAX_PERCENT = Decimal("100")

#: Vigencia maxima admitida para una cotizacion, en dias.
MAX_VALIDITY_DAYS = 3650


class VersionedSingletonMixin(TimestampMixin):
    """Fila unica con control de concurrencia optimista.

    ``version`` se incrementa en cada actualizacion. El cliente devuelve la
    version que leyo; si no coincide con la almacenada, la escritura se rechaza
    en vez de pisar en silencio un cambio mas reciente.
    """

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=SINGLETON_ID)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class CompanySettings(Base, VersionedSingletonMixin):
    """Identidad de la empresa y textos que alimentaran los documentos."""

    __tablename__ = "company_settings"

    # ---- Identificacion -------------------------------------------------
    legal_name: Mapped[str | None] = mapped_column(String(200))
    trade_name: Mapped[str | None] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(20))

    # ---- Domicilio ------------------------------------------------------
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    ubigeo_code: Mapped[str | None] = mapped_column(
        ForeignKey("ubigeo_districts.code", ondelete="RESTRICT"),
    )
    district: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(120))
    department: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))

    # ---- Contacto -------------------------------------------------------
    phone: Mapped[str | None] = mapped_column(String(40))
    mobile: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_role: Mapped[str | None] = mapped_column(String(120))

    # ---- Identidad visual -----------------------------------------------
    #: Ruta del objeto dentro del bucket. El binario nunca se guarda aqui.
    logo_object_path: Mapped[str | None] = mapped_column(String(400))
    logo_content_type: Mapped[str | None] = mapped_column(String(80))
    logo_size_bytes: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        CheckConstraint("version > 0", name="version_positive"),
    )


class CommercialSettings(Base, VersionedSingletonMixin):
    """Parametros comerciales y textos por defecto de los documentos."""

    __tablename__ = "commercial_settings"

    #: Codigo ISO 4217. No se precarga ningun valor: lo define el usuario.
    currency_code: Mapped[str | None] = mapped_column(
        ForeignKey("currency_catalog.code", ondelete="RESTRICT"),
    )
    currency_symbol: Mapped[str | None] = mapped_column(String(8))

    #: Porcentaje, no fraccion: 18 significa 18 %. NUMERIC, nunca float.
    tax_percent: Mapped[Decimal | None] = mapped_column(percentage_numeric())

    #: Vigencia por defecto de una cotizacion, en dias.
    quote_validity_days: Mapped[int | None] = mapped_column(Integer)

    # ---- Textos de documentos (texto plano, jamas HTML) ------------------
    general_conditions: Mapped[str | None] = mapped_column(Text)
    payment_notes: Mapped[str | None] = mapped_column(Text)
    document_footer: Mapped[str | None] = mapped_column(Text)

    bank_accounts: Mapped[list[BankAccount]] = relationship(
        back_populates="settings",
        cascade="all, delete-orphan",
        order_by="BankAccount.id",
    )

    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            f"tax_percent IS NULL OR (tax_percent >= 0 AND tax_percent <= {MAX_TAX_PERCENT})",
            name="tax_percent_range",
        ),
        CheckConstraint(
            f"quote_validity_days IS NULL OR "
            f"(quote_validity_days > 0 AND quote_validity_days <= {MAX_VALIDITY_DAYS})",
            name="quote_validity_range",
        ),
        CheckConstraint(
            "currency_code IS NULL OR currency_code ~ '^[A-Z]{3}$'",
            name="currency_code_iso4217",
        ),
    )


class BankAccount(Base, TimestampMixin):
    """Cuenta bancaria para instrucciones de pago.

    Vive en su propia tabla y no como columnas de ``commercial_settings``
    porque es un grupo repetible: el dia que el taller use una segunda cuenta,
    basta insertar una fila en vez de migrar el esquema. La Fase 2 expone
    unicamente la cuenta principal, de modo que la interfaz sigue siendo un
    formulario simple.
    """

    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    settings_id: Mapped[int] = mapped_column(
        ForeignKey("commercial_settings.id", ondelete="CASCADE"),
        nullable=False,
    )

    bank_name: Mapped[str | None] = mapped_column(String(160))
    account_holder: Mapped[str | None] = mapped_column(String(200))
    account_number: Mapped[str | None] = mapped_column(String(64))
    #: Codigo de Cuenta Interbancario (Peru), 20 digitos.
    cci: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    settings: Mapped[CommercialSettings] = relationship(back_populates="bank_accounts")

    __table_args__ = (
        # Como maximo una cuenta principal. El indice parcial deja el resto
        # libre para las cuentas adicionales de fases futuras.
        Index(
            "uq_bank_accounts_primary",
            "is_primary",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )
